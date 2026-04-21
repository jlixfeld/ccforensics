---
title: ccforensics — Design Specification
date: 2026-04-21
status: approved
version: 0.1
---

# ccforensics — Design Specification

**Date:** 2026-04-21
**Status:** Approved (sections 1–7)
**Owner:** jason@lixfeld.ca

## 0. Problem and scope

Existing Claude Code usage tools (ccusage, ccost, claude-devtools, lm-assist, claude-code-log) provide session-level and turn-level cost breakdowns. None answer the question ccforensics targets: **which of my installed plugins, skills, and subagents are driving my token costs, and are they worth what they cost?**

Full problem statement is reproduced under `docs/specs/problem-statement.md` and is normative. This document is the design that implements it.

### In scope

Per problem statement §Scope:

1. Parse all JSONL session files under `~/.claude/projects/<encoded-path>/*.jsonl`.
2. Deduplicate messages across files (same UUID can appear in multiple files on branching/resume).
3. Build parent→child trees from `parentToolUseId` / `sourceToolUseID` / `agentId`.
4. Attribute tokens + cost to each node.
5. Per-subagent-type aggregates (count, cost, avg, model).
6. Discover installed plugins via `~/.claude/plugins/cache/*/`; map agents/skills → owning plugin.
7. Roll up subagent cost to plugin level.
8. Detect skill activations: `Read` of `SKILL.md`, `Skill`-tool invocation, SessionStart hook injection.
9. Attribute a share of cache-read cost to active skills (estimate ± band, not accounting precision).
10. Correlate skill activations with downstream `Task`/`Agent` spawns within N turns (heuristic).
11. Session-listing command for discovery.
12. Per-session + aggregate reports; JSON/CSV export.

### Out of scope

- Deterministic causal attribution for plugin hook scripts (interpreting script source to explain why a hook fired).
- Subagent output quality measurement.
- Real-time monitoring.
- Web UI or dashboard.
- Replacing ccusage / claude-devtools for what they already do well.

### Ambiguities — documented, not hidden

- **Cache-read share attribution is an estimate.** Reports show ± bands, never false-precision figures.
- **Name collision** between skill and subagent (e.g., `pr-review-loop` exists in both): cost attributes via the subagent tool-use chain (precise); skill's context-carry is reported separately.
- **Double-attribution is worse than under-attribution.** Un-attributable cost lands in an explicit "unattributed main agent work" bucket rather than being guessed at.

## 1. Package layout

```
ccforensics/
├── pyproject.toml              # uv-managed; entrypoint = ccforensics.cli:main
├── README.md
├── docs/
│   └── specs/
│       ├── design.md           # this file
│       └── problem-statement.md
├── src/ccforensics/
│   ├── __init__.py
│   ├── cli.py                  # click commands
│   ├── paths.py                # ~/.claude resolution; cwd decoding; cache dir
│   ├── models.py               # pydantic types, versioned + permissive
│   ├── jsonl.py                # streaming parser; dedup
│   ├── pricing.py              # LiteLLM fetch + cache; fuzzy model-name resolver
│   ├── tree.py                 # parent→child reconstruction (main + subagent JSONLs)
│   ├── registry.py             # plugin manifest scan + user-level artifact scan
│   ├── attribution.py          # cost → bucket (main | subagent-type | plugin | skill)
│   ├── skills.py               # activation detection (3 channels) + ± band estimator
│   ├── index.py                # SQLite schema + reconcile-by-(path,mtime,size)
│   ├── report/
│   │   ├── session.py
│   │   ├── session_list.py
│   │   ├── aggregate.py
│   │   └── plugins.py
│   └── export.py               # JSON/CSV writers
└── tests/
    ├── fixtures/               # synthetic JSONL + redacted real samples
    ├── integration/
    └── test_*.py
```

**Dependency policy:** pragmatic. Reach for a small dep when it meaningfully beats stdlib. Committed set: `click`, `rich`, `httpx`, `pydantic`, `platformdirs`. Add-hoc additions require justification in the PR.

## 2. SQLite index

**Path:** `~/.cache/ccforensics/index.sqlite` (via `platformdirs.user_cache_dir`).
**Versioning:** `PRAGMA user_version`; migrations as ordered DDL in `ccforensics/index.py`.

### Schema

```sql
CREATE TABLE files (
    path              TEXT PRIMARY KEY,
    mtime_ns          INTEGER NOT NULL,
    size              INTEGER NOT NULL,
    session_id        TEXT NOT NULL,
    kind              TEXT NOT NULL,          -- 'main' | 'subagent'
    agent_id          TEXT,
    schema_version    TEXT,
    parse_warnings    INTEGER NOT NULL DEFAULT 0,
    last_parsed_at    INTEGER NOT NULL
);
CREATE INDEX idx_files_session ON files(session_id);

CREATE TABLE messages (
    dedup_key                 TEXT PRIMARY KEY,
    file_path                 TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    session_id                TEXT NOT NULL,
    uuid                      TEXT,
    parent_uuid               TEXT,
    source_tool_use_id        TEXT,
    source_tool_assistant_uuid TEXT,
    tool_use_id               TEXT,
    tool_name                 TEXT,
    agent_id                  TEXT,
    role                      TEXT NOT NULL,
    type                      TEXT NOT NULL,
    model                     TEXT,
    ts                        INTEGER NOT NULL,
    is_sidechain              INTEGER NOT NULL DEFAULT 0,
    is_meta                   INTEGER NOT NULL DEFAULT 0,
    input_tokens              INTEGER,
    output_tokens             INTEGER,
    cache_creation            INTEGER,
    cache_read                INTEGER,
    cost_usd                  REAL,
    raw_pointer               INTEGER
);
CREATE INDEX idx_messages_session ON messages(session_id);
CREATE INDEX idx_messages_tool_use_id ON messages(tool_use_id);
CREATE INDEX idx_messages_source_tool ON messages(source_tool_use_id);
CREATE INDEX idx_messages_agent ON messages(agent_id);
CREATE INDEX idx_messages_ts ON messages(ts);

CREATE TABLE subagent_spawns (
    spawn_id                   TEXT PRIMARY KEY,    -- parent tool_use_id
    parent_session_id          TEXT NOT NULL,
    parent_message_dedup_key   TEXT NOT NULL REFERENCES messages(dedup_key),
    child_agent_id             TEXT,
    child_file_path            TEXT,
    subagent_type              TEXT,
    description                TEXT,
    model                      TEXT,
    ts_spawned                 INTEGER NOT NULL,
    ts_returned                INTEGER,
    total_cost_usd             REAL,
    total_input                INTEGER,
    total_output               INTEGER,
    total_cache_create         INTEGER,
    total_cache_read           INTEGER
);
CREATE INDEX idx_spawns_session ON subagent_spawns(parent_session_id);
CREATE INDEX idx_spawns_type ON subagent_spawns(subagent_type);

CREATE TABLE skill_activations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id              TEXT NOT NULL,
    skill_path              TEXT NOT NULL,         -- absolute path to SKILL.md
    skill_name              TEXT NOT NULL,
    plugin_name             TEXT,                  -- NULL = user-level
    source                  TEXT NOT NULL,         -- 'read' | 'hook-injection' | 'skill-tool'
    activated_at            INTEGER NOT NULL,
    activated_by_dedup_key  TEXT,
    content_size            INTEGER,
    estimated_cost_usd      REAL,
    estimated_cost_band_usd REAL
);
CREATE INDEX idx_skills_session ON skill_activations(session_id);
CREATE INDEX idx_skills_plugin ON skill_activations(plugin_name);

CREATE TABLE plugins (
    name           TEXT PRIMARY KEY,
    version        TEXT,
    install_path   TEXT NOT NULL,
    scope          TEXT,
    manifest_json  TEXT
);

CREATE TABLE user_level_artifacts (
    path  TEXT PRIMARY KEY,
    kind  TEXT NOT NULL,                           -- 'agent' | 'skill'
    name  TEXT NOT NULL
);

CREATE TABLE session_summaries (
    session_id       TEXT PRIMARY KEY,
    project_path     TEXT,
    project_display  TEXT,
    started_at       INTEGER NOT NULL,
    last_active_at   INTEGER NOT NULL,
    duration_s       INTEGER NOT NULL,
    turn_count       INTEGER NOT NULL,
    total_cost_usd   REAL,
    summary_text     TEXT,
    summary_source   TEXT                          -- 'claude-summary' | 'first-prompt' | 'none'
);

CREATE TABLE session_rollups (
    session_id    TEXT NOT NULL,
    bucket_kind   TEXT NOT NULL,                   -- 'main' | 'subagent-type' | 'plugin' | 'skill' | 'unattributed'
    bucket_name   TEXT NOT NULL,
    cost_usd      REAL NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_create  INTEGER NOT NULL,
    cache_read    INTEGER NOT NULL,
    PRIMARY KEY (session_id, bucket_kind, bucket_name)
);
```

### Reconciliation

1. Walk `~/.claude/projects/**/*.jsonl` including `*/subagents/*.jsonl`.
2. Compare `(path, mtime_ns, size)` against `files` row. Unchanged → skip. Changed or new → re-parse; cascade-delete prior rows in `messages`, `subagent_spawns`, `skill_activations` for this `path` first.
3. After messages are reconciled, recompute `subagent_spawns`, `session_summaries`, `session_rollups` for affected sessions only.
4. `plugins` and `user_level_artifacts` rebuild every run (tiny + cheap).

Every report command triggers reconciliation by default. `--no-refresh` skips it. `ccforensics index --rebuild` drops and recreates everything.

## 3. Parser and tree reconstruction

### 3.1 JSONL parser (`jsonl.py` + `models.py`)

- Stream line-by-line. `json.loads()` failure on non-final line → warn + continue. Failure on final line at EOF → silent skip (issue #20612: transcripts sometimes not flushed).
- Pydantic `TranscriptEntry` is a discriminated union on `type` with `Extra.allow`. Required fields: only `type` and `timestamp`. Subclasses cover `user`, `assistant`, `system`, `attachment`, `summary`, `file-history-snapshot`. `UnknownEntry` catches everything else.
- Field normalization at parse time: `sourceToolUseID | parentToolUseId → source_tool_use_id`; `sourceToolAssistantUUID | parentToolAssistantUuid → source_tool_assistant_uuid`. Token fields keep observed `message.usage` snake_case.
- Unknown `type` values (`permission-mode`, `last-prompt`, `queue-operation`, future additions): increment `files.parse_warnings` once per session, `-v` log, row retained but marked non-billable.

### 3.2 Deduplication

Tiered keys with prefix (prevents cross-tier collisions):

```python
def dedup_key(entry) -> str | None:
    mid = entry.message.id if entry.message else None
    rid = entry.requestId
    sid = entry.sessionId
    if mid and rid: return f"req:{mid}:{rid}"
    if mid and sid: return f"session:{mid}:{sid}"
    return None  # entry inserted with synthetic key file:{path}:{lineno}
```

### 3.3 Cost annotation

At parse time, each message has `cost_usd` computed via `pricing.py`. NULL when model resolution fails; aggregates note "(pricing unavailable for model X)".

### 3.4 Tree reconstruction

**Pass 1 — within-session.** Index `messages` by `tool_use_id` (from `tool_use` content blocks). For each record carrying `source_tool_use_id`, link to the parent via `tool_use_id == source_tool_use_id`.

**Pass 2 — cross-file subagent linkage.** For each subagent JSONL (`<session>/subagents/agent-<agentId>.jsonl`):

- Read sibling `agent-<agentId>.meta.json` → `{agentType, description}` (authoritative).
- Parent `tool_use` (in parent session JSONL) located by heuristic: nearest `tool_use name=Agent|Task` before child's `ts_spawned` within same session; tie-break by `input.subagent_type == meta.json::agentType`; if still tied, nearest timestamp.
- Unresolvable → warning + row written with `parent_message_dedup_key=NULL`; cost lands in `unattributed` bucket.
- Subagent-within-subagent nesting supported (recursion during rollup).

### 3.5 Schema drift escape hatches

- Future `version` field → log and continue; `-v` shows a `⚠` marker in listing.
- `KNOWN_TYPES` registry; anything outside triggers once-per-session warning.
- Missing `source_tool_use_id` on records that should have one (carry `toolUseResult`) → warning + cost routed to `unattributed`.

### 3.6 Explicit non-goals

- No deep content-block interpretation. `tool_use` inputs queried only for `name`, `id`, `input.subagent_type`.
- No reading plugin hook source code. Hook injection events recorded; the *reason* the hook fired is out of scope.

## 4. Pricing, attribution, skill detection

### 4.1 Pricing (`pricing.py`)

- Source: `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`.
- Disk cache: `~/.cache/ccforensics/litellm.json`, 24h TTL. Stale-but-exists on fetch failure with warning. No-cache + no-network → hardcoded fallback covering Sonnet 4.5, Haiku 4.5, Opus 4.1, Opus 4.5, Sonnet 4, 3.5 Haiku.
- Model-name resolution (ccusage pattern): exact match → prefixed variants (`anthropic/`, `claude-3-5-`, `claude-3-`, `claude-`) → case-insensitive substring.
- Required fields per model: `input_cost_per_token`, `output_cost_per_token`, `cache_creation_input_token_cost`, `cache_read_input_token_cost`. Missing cache fields → fall back to ccost's ratios (creation = 25% of input; read = 10% of input) + one-time warning per model.
- Cost formula:
  ```
  cost = input_tokens * input_cost
       + output_tokens * output_cost
       + cache_creation * cache_creation_cost
       + cache_read * cache_read_cost
  ```
- Tiered `_above_200k` pricing applied only when present in LiteLLM data. Deferred otherwise.

### 4.2 Attribution buckets (`attribution.py`)

| Bucket | Rule |
|---|---|
| `main` | Message in main session JSONL, not inside any subagent tool-use subtree. |
| `subagent:<type>` | Message in subagent JSONL (`kind='subagent'`). `<type>` = `meta.json::agentType` else parent `tool_use.input.subagent_type`. |
| `unattributed` | Subagent JSONL with no resolvable parent, or message with unparseable linkage. |

**Plugin rollup:** for each `subagent:<type>`, look up `<type>` in the registry. Result: plugin name | `user-level` | `builtin` (for `general-purpose`, `Explore`, `Plan`, `statusline-setup`).

**Hard invariant (asserted):**
```
session_total_cost == sum(main + all subagent:<type> + unattributed)
```

### 4.3 Skill activation detection (`skills.py`)

Three channels, priority order:

- **A. `Skill` tool call.** Assistant `tool_use` where `name == "Skill"`. Direct invocation. `source='skill-tool'`.
- **B. Read of `**/SKILL.md`.** Assistant `tool_use` where `name == "Read"` and `input.file_path` matches `*/skills/*/SKILL.md` under `~/.claude/skills/` or `~/.claude/plugins/cache/.../skills/`. `source='read'`.
- **C. Hook injection.** `attachment` record with `attachment.type == "hook_success"` AND `attachment.hookEvent == "SessionStart"` AND `attachment.stdout` JSON contains `hookSpecificOutput.additionalContext`. Skill detection via heuristic regex on `additionalContext`: `/---\s*\nname:\s*[\w-]+/`, `<session-start-hook>`, or `'using-superpowers'`. Skill name from YAML frontmatter. `source='hook-injection'`.

**Attribution anchor:** full `SKILL.md` path (never name alone). Path unambiguously resolves plugin-vs-user-level. On duplicate names across locations, tool emits a one-time warning listing the duplicates.

### 4.4 Context-carry ± band

Skill loaded at turn T with content size S (bytes, ÷4 as token proxy) is "carried" in the prompt until compaction or eviction. Each subsequent assistant turn re-pays `cache_read` cost including the skill.

Per subsequent assistant turn T+k with `cache_read = R_k` at `total_context = cache_read + cache_creation + input_tokens`:

```
skill_share_k = R_k * (S / total_context_at_Tk)
```

Summed across turns until compaction (large `cache_read` drop OR `isCompactSummary` marker) or session end. Multiplied by per-turn cache-read price.

**Band:**
- **Lower:** skill evicts on first compaction (assume 50% of turns before compact).
- **Upper:** skill survives every compaction.
- **Point:** midpoint, written to `estimated_cost_usd`; half-width to `estimated_cost_band_usd`.

Reports always display as `$X.XX ± $Y.YY` — explicit about uncertainty.

### 4.5 Name-collision rule

Skill and subagent sharing a name (e.g., `pr-review-loop`): cost attributes via the tool-use chain (precise); skill's context-carry reported separately in the ledger. Report footnotes the collision.

### 4.6 Downstream spawn correlation

Per skill activation at turn T, list `subagent_spawns` with `ts_spawned` in the next N turns (default N=5, `--correlate-spawns N` flag). Caveat rendered: "correlation, not causation."

## 5. CLI surface

### 5.1 Commands

```
ccforensics session --list              [--project P] [--since D] [--until D]
                                        [--grep PAT] [--sort KEY] [--reverse]
                                        [--limit N] [--json | --csv] [--no-refresh]

ccforensics session <id-or-path>        [--include-unattributed] [--skill-ledger]
                                        [--correlate-spawns N] [--json | --csv]
                                        [--no-refresh]

ccforensics aggregate                   [--since D] [--until D] [--project P]
                                        [--group-by KEY] [--json | --csv] [--no-refresh]

ccforensics plugins                     [--since D] [--until D]
                                        [--json | --csv] [--no-refresh]

ccforensics index --rebuild             # drop + recreate SQLite
ccforensics index --stats               # counts + last-refresh
```

Global flags: `-v/--verbose`, `--cache-dir PATH`, `--pricing-refresh`.

### 5.2 `session --list`

- Default sort: `last-active` desc. `--sort`: `cost|started|last-active|turns`. `--reverse` flips.
- Session argument: full UUID | unique UUID prefix (≥ 6 chars) | absolute `.jsonl` path. Ambiguous prefix → error with match list.
- **UUID** column: first 6 chars, auto-extended if collision within result set.
- **Summary** column: full text, wrapped to remaining terminal width via `rich.Table` multi-line cells. No 80-char truncation.
- **Dur** column: human format (`4h12m`, `47m`, `2d3h`).
- **Cost** column: `$0.00` for no-billable, `$—` for no-pricing.
- **Project** column: decoded cwd's last segment (preferred) or dir-decoded; truncate to 30 chars.
- **Summary source badge** in `-v`: `[C]` claude-summary, `[F]` first-prompt, `[-]` none.
- `--grep`: case-insensitive substring on `summary_text`. No regex.
- `--since`/`--until`: `YYYY-MM-DD` | `Nd` | `today` | `yesterday`.

**Summary extraction priority:**
1. Claude-emitted summary (`type: "summary"` w/ `leafUuid` matching a message in this session, or `isCompactSummary`; most recent if multiple).
2. First user prompt — `<command-name>...` wrappers stripped; IDE attachments → emoji (`📎 /path`); hook-injection blobs skipped; newlines collapsed; 1000-char store, full display (wrapped).
3. `<no summary available>`.

### 5.3 `session <id>` report

Five sections (full layout in doc history §5.3; summary here):

1. Header: project, started, last-active, duration, turns, models, total cost (± skill band).
2. **Cost by bucket**: main, each `subagent:<type>`, unattributed (with count of unresolved spawns).
3. **Cost by plugin**: per-plugin rollup, plus `user-level`, `builtin`, `none`.
4. **Skill ledger**: per activation — turn, channel, content-carry ± band, downstream spawns in N turns, collision footnote.
5. **Parse notes**: schema version seen, warning counts.

### 5.4 `aggregate`

Same bucket breakdown summed across window. `--group-by`: `none` | `project` | `day` | `week` | `month` | `plugin`.

### 5.5 `plugins`

All-time per-plugin rollup. Columns: plugin, total cost, session count, most-used subagent-type, most-used skill, first-seen, last-seen. Sort: total cost desc.

### 5.6 Export

- `--json`: structured, nested, stable shape documented in `docs/schema.md`.
- `--csv`: flat (one row per bucket for `session`; one per session for `--list`; one per `(session, plugin)` for `plugins`).
- When export is on, non-data output → stderr; pure data → stdout.

### 5.7 UX

- First run / `index --rebuild`: `rich.Progress` bar.
- Subsequent runs: silent unless `-v`. No-change reconciliation is near-instant.
- `index --stats`: file count, row counts per table, last-refresh, disk size.

## 6. Testing

### 6.1 Pyramid

- **Unit:** one test module per `src/ccforensics/` module. Fixtures under `tests/fixtures/` (synthetic JSONL corpora + frozen LiteLLM snapshot).
- **Integration:** end-to-end CLI via `click.testing.CliRunner`; exported JSON/CSV snapshot-tested via `syrupy`.
- **Invariant tests:** `sum(buckets) == session_total`; dedup determinism under input reorder; every spawn resolves or lands in unattributed (no double-count).
- **Ground-truth cost:** one redacted real JSONL in `tests/fixtures/real/`; totals match ccusage to ±1%.

### 6.2 Explicit non-targets

- No real-network LiteLLM calls in tests; mock at `httpx.Client`.
- No Claude Code live-emission tests; covered via synthetic fixtures + defensive parser.
- No terminal pixel-level tests; data shape only.

### 6.3 CI

Single GH Actions workflow: `uv sync`, `pytest -x --cov`, `ruff check`, `ruff format --check`, `mypy src/` strict. Matrix: Python 3.11–3.13; macOS + Linux. Coverage gate: ≥ 85%, explicit `pragma: no cover` with reason for exceptions.

### 6.4 Success-criteria verification

| SC | Verification |
|---|---|
| SC1: $109 broken down by main/subagent/plugin | Run `ccforensics session <uuid>` on 2026-04-16 session; total within ±$0.50; recognized plugins present. |
| SC2: Skills + context-carry | Skill ledger present with turn, channel, ± band. Superpowers hook at turn 1 detected. |
| SC3: Spawn clustering around skill activations | "Downstream spawns" correlation under each skill. |
| SC4: Two actionable workflow changes | Outcome test, manual. If reports don't support the conclusion, iterate. |
| SC5: Identify past session in ≤30s from `--list` | Manual timing; tune summary extraction if it fails. |
| SC6: Pipe UUID into `session <uuid>` | Covered by prefix matching + CLI smoke tests. |

### 6.5 Pre-release manual pass

1. `index --rebuild` on real corpus; capture warning count.
2. `session --list`; eyeball all columns.
3. 3 recent remembered sessions → deep report sanity-check.
4. `plugins`; rank against intuition.
5. `aggregate --since 30d --group-by project`; should include ccforensics itself.

## 7. Build sequencing

### 7.1 Milestones

- **M0** — Repo skeleton: `uv init`, `pyproject.toml` entrypoint, ruff/mypy/pytest configs, CI skeleton, fixtures scaffold, tracking issue + feature branch.
- **M1** — Parser + dedup (no CLI yet). Unit tests green; invariant-style dedup test.
- **M2** — Pricing + cost annotation. Ground-truth vs ccusage within ±1%.
- **M3** — SQLite index + reconciliation. `index --rebuild` and `index --stats`. No-change reconcile < 1s.
- **M4** — `session --list`. SC5 verified.
- **M5** — Tree reconstruction + bucket attribution.
- **M6** — Plugin registry + plugin rollup. Collision warning emitted.
- **M7** — `session <id>` deep report. SC1 verified.
- **M8** — Skill detection + context-carry ± + spawn correlation. SC2 + SC3 verified.
- **M9** — `aggregate` + `plugins` commands.
- **M10** — Polish, README, manual pass, v0.1.0 tag, `uv tool install` end-to-end. SC4 verified (or iterate).

### 7.2 Repo bootstrap

1. `git init` + `.gitignore` (Python + macOS + `.venv` + `*.sqlite` + `.pytest_cache`).
2. `gh repo create jlixfeld/ccforensics --public --source=. --remote=origin --push`.
3. `gh issue create --title "Initial implementation of Claude Code usage forensics tool" --body-file docs/specs/design.md`.
4. Feature branch `feature/initial-implementation` (or worktree).

### 7.3 Risks

| Risk | Mitigation |
|---|---|
| JSONL schema drift | Defensive parser (§3.5); `test_schema_drift.py`. |
| LiteLLM misses new Claude IDs | Fuzzy resolver + hardcoded fallback + `--pricing-refresh`. |
| Subagent linkage heuristic false-matches | `meta.json::agentType` tie-break; `-v` logs each decision; multi-candidate fixtures. |
| Skill ± band too wide to be useful | Show rank-ordering alongside $; document assumption in report footer. |
| SQLite schema change mid-dev | `PRAGMA user_version` migration pattern from M3. |
| Unexpected real JSONL records | M1 demo = ingest real files without crash as gate. |

## Appendix A — Research sources

Consulted during design (parallel sub-agent research, 2026-04-21):

- **ccusage** (`ryoppippi/ccusage`, TS) — dedup hash, LiteLLM URL, fuzzy model resolver patterns.
- **ccost** (`carlosarraes/ccost`, Rust) — tiered prefixed dedup keys, cache-field ratio fallbacks.
- **claude-devtools** (`matt1398/claude-devtools`, Electron/TS) — first-user-prompt extraction, path decoder w/ cwd fallback, `type:"summary"` schema confirmation.
- **lm-assist** (`langmartai/lm-assist`, Node) — filesystem-probing path decode, namespaced `plugin:skill` parsing, cached skill-invocation span model.
- **claude-code-log** (`daaain/claude-code-log`, Python) — cross-session summary matching via `leafUuid`, three-tier title fallback, file-based cache keyed on mtime.
- **Anthropic docs** (`docs.claude.com`, `github.com/anthropics/claude-code`) — authoritative plugin manifest, `SKILL.md`, agent frontmatter, hook event contracts. Session JSONL itself is **not** officially documented; parse defensively.
- **GitHub issues** — #25032, #23614, #26123, #26485, #29331 (sessions-index staleness); #20612, #22900 (unflushed transcripts); #36583 (`file-history-snapshot.messageId` collision); #32175 (feature request: subagent parent-session metadata).

## Appendix B — Key observations from user's local environment

- Claude Code 2.1.x on disk. Field name drift: **`sourceToolUseID` / `sourceToolAssistantUUID`** observed, not `parentToolUseId`.
- Subagent layout: `<sessionId>/subagents/agent-<agentId>.jsonl` + sibling `.meta.json`.
- SessionStart hook injection pattern: `type:"attachment"` → `attachment.type:"hook_success"` → `attachment.stdout` JSON with `hookSpecificOutput.additionalContext` containing YAML-frontmatter skill content (Superpowers bootstrap is the worked example).
- Additional seen types: `permission-mode`, `file-history-snapshot`, `last-prompt`, `attachment`.
- `<sessionId>/tool-results/` sibling dir exists — unexplored; not a blocker for M1–M10 but worth revisiting.
- Installed plugins: `claude-plugins-official` (11 plugins including `superpowers@5.0.7`), `jlixfeld-claude-skills` (2: `auto-review`, `pr-review-loop`), `openai-codex`.
- User-level skills at `~/.claude/skills/`: `build`, `codebase-audit`, `napkin-notes`, `pr-quality`, `pr-review-loop`, `project-memory`.
- Known collision: `pr-review-loop` (user-level + plugin). Plausibly pre-plugin leftover.
