---
title: Cache efficiency, per-tool/per-MCP report, service-tier capture — Design
date: 2026-05-02
status: draft
version: 0.1
supersedes: none
extends: docs/specs/design.md
---

# Cache efficiency, per-tool/per-MCP report, service-tier capture

**Date:** 2026-05-02
**Status:** Draft (pending user review)
**Owner:** jason@lixfeld.ca

## 0. Motivation and scope

Three additive features extend ccforensics with metrics that codeburn surfaces and ccforensics currently lacks, without violating the charter's "no false precision" rule:

1. **Cache efficiency** — surface how much of the bill is offset by prompt-cache reuse. Pure ratio of values already stored; exact.
2. **Per-tool / per-MCP spend** — show which tools and MCP servers dominate spending. Honest framing using *isolated* (precise) and *shared exposure* (upper-bound) cost columns. No proportional-split fabrication.
3. **Service-tier capture** — record `usage.service_tier` from every message so a future fast-mode pricing branch has data to verify against. No pricing change in this spec.

### In scope

- Schema v2 → v3 migration adding `messages.service_tier` and a new `message_tool_uses` table.
- Reconcile-pipeline change to capture every tool_use block (today only the first per assistant message is indexed).
- Cache-efficiency columns in `session show` and `aggregate` reports.
- New `tools` CLI subcommand with default server-rolled view, `--detail` for per-tool drill-down, JSON/CSV output.
- Service-tier breakdown surface (read-only), no pricing math.

### Out of scope

- Fast-mode pricing multiplier. Deferred until a real fast-mode session exists in the corpus to verify the signal end-to-end. This spec documents the follow-up checklist (§6).
- Per-bucket or per-tool cache efficiency. Cache reuse is request-level, not bucket- or tool-level; reporting it that way would mislead.
- Task categorization, one-shot rate, retry-cycle detection. Outside the cost-attribution charter — explicitly declined during brainstorm.
- Real-time monitoring, web UI, dashboard.

### Charter alignment

- Bucket-attribution invariant `Σ(session_rollups.cost_usd) == Σ(messages.cost_usd)` is unaffected — the per-tool report is an orthogonal slicing, not a bucket.
- Per-tool numbers are presented with explicit precision labels: `isolated_cost_usd` is exact; `shared_exposure_usd` is an upper bound and must not be summed across rows.
- Cache-efficiency numbers are exact arithmetic over stored values; no estimation.
- Service-tier capture stores precise data points; no pricing inference.

## 1. Architecture

```
JSONL → jsonl.parse → annotate_cost → index.write_message
                                          │
                                          ├─→ messages (+ service_tier col)
                                          └─→ message_tool_uses (NEW: 1 row per tool_use block)

reports/
  ├── session.py     (+ cache_eff_pct, cache_savings_usd cols, + service_tier breakdown line)
  ├── aggregate.py   (+ cache_eff_pct, cache_savings_usd cols, + service_tier breakdown line)
  └── tools.py       (NEW: per-tool & per-MCP-server analysis)
```

**Modules touched:**

- `models.py` — explicit `service_tier: str | None = None` field on `UsageBlock`.
- `index.py` — schema v3 migration; replace `break`-after-first-tool_use with loop populating `message_tool_uses`.
- `report/session.py` — add cache-eff columns + service-tier breakdown footer.
- `report/aggregate.py` — same.
- `report/tools.py` — new module.
- `report/__init__.py` — wire up new module.
- `report/_cache.py` — new shared helper for cache metrics.
- `cli.py` — new `tools` command.
- `tests/` — new test files per §7.

**Modules untouched:** `tree.py`, `registry.py`, `skills.py`, `paths.py`, `attribution.py`, `export.py`, all existing report-command behavior.

## 2. Data model (schema v3)

### `messages` — add column

```sql
ALTER TABLE messages ADD COLUMN service_tier TEXT;
```

Nullable. NULL = pre-v3 row not yet re-reconciled, OR usage block had no `service_tier` field. Standard sessions populate `'standard'` on next reconcile of their file (mtime-driven).

### New `message_tool_uses` table

```sql
CREATE TABLE message_tool_uses (
    message_dedup_key  TEXT NOT NULL REFERENCES messages(dedup_key) ON DELETE CASCADE,
    ordinal            INTEGER NOT NULL,    -- position within message.content (0-based)
    tool_use_id        TEXT NOT NULL,
    tool_name          TEXT NOT NULL,
    mcp_server         TEXT,                -- NULL for native tools
    args_size_bytes    INTEGER NOT NULL,    -- len(json.dumps(input)) — precise size proxy
    PRIMARY KEY (message_dedup_key, ordinal)
);
CREATE INDEX idx_mtu_tool_name ON message_tool_uses(tool_name);
CREATE INDEX idx_mtu_mcp_server ON message_tool_uses(mcp_server) WHERE mcp_server IS NOT NULL;
```

### Why one row per tool_use, not a JSON blob

Indexed group-by/filter queries (the `tools` report) need rows. JSON blob would force scan-and-parse per query.

### `messages.tool_name` / `messages.tool_use_id` retained

These columns are load-bearing for `tree.discover_spawn` (parent-link recompute uses them). They continue to store the *first* tool_use's name/id, exactly as today. Sibling tool_uses are recorded only in `message_tool_uses`. Spawn-discovery code path is unchanged.

### `mcp_server` derivation

At write time:

```python
mcp_server = (
    tool_name.split("__", 2)[1]
    if tool_name.startswith("mcp__") and tool_name.count("__") >= 2
    else None
)
```

Stored, not derived per-query. Malformed names (single `__`, no server segment) treated as native tools (`mcp_server = NULL`).

### Cold-backfill mechanism

The v3 migration ends with:

```sql
UPDATE files SET mtime_ns = 0;
```

Next `reconcile_projects_dir` walk sees mismatch on every file → re-parse → repopulates `message_tool_uses` and `service_tier`. One-shot, automatic on first command after upgrade. No new flag. Same approach used implicitly by previous schema bumps.

## 3. Reconcile pipeline change

### `models.py`

```python
class UsageBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    service_tier: str | None = None  # NEW
```

### `index.py::write_message`

Replace the single-match block:

```python
tool_use_id = None
tool_name = None
tool_uses_for_aux: list[tuple[int, str, str, object]] = []

if msg and msg.content:
    for ordinal, block in enumerate(msg.content):
        if block.type == "tool_use":
            if tool_use_id is None:
                tool_use_id = block.id
                tool_name = block.name
            tool_uses_for_aux.append((ordinal, block.id, block.name, block.input))

# Existing INSERT OR REPLACE INTO messages (with service_tier added to param list).
# Then:
for ordinal, tu_id, tu_name, tu_input in tool_uses_for_aux:
    mcp_server = (
        tu_name.split("__", 2)[1]
        if tu_name.startswith("mcp__") and tu_name.count("__") >= 2
        else None
    )
    try:
        args_size = len(
            json.dumps(tu_input, sort_keys=True, separators=(",", ":")).encode()
        )
    except (TypeError, ValueError):
        args_size = 0  # defensive; pydantic-shaped input should always serialize
    conn.execute(
        """INSERT OR REPLACE INTO message_tool_uses
           (message_dedup_key, ordinal, tool_use_id, tool_name, mcp_server, args_size_bytes)
           VALUES (?,?,?,?,?,?)""",
        (key, ordinal, tu_id, tu_name, mcp_server, args_size),
    )
```

### `service_tier` capture

Add `usage.service_tier if usage else None` to the existing `INSERT OR REPLACE INTO messages` parameter list.

### Purge semantics

`reconcile_file` already purges stale `messages` rows for the file before re-insert. `message_tool_uses` uses `ON DELETE CASCADE`, so the existing purge cleans it transitively. No new purge code.

### Performance

Typical assistant turn: 0–5 tool_use blocks. Cold-backfill on a ~30-day corpus is bounded by existing reconcile time plus small per-tool insert overhead. SQLite bulk-insert in the same transaction as the parent message row.

## 4. Cache efficiency reporting

### Formulas (both exact)

```
cache_savings_usd = cache_read_tokens × (input_price_per_token − cache_read_price_per_token)

cache_eff_pct     = (cache_read × read_price)
                  / (cache_read × read_price
                     + cache_creation × create_price
                     + input × input_price)
                  × 100
```

`cache_savings_usd` answers "what would I have paid without cache reuse?" Exact, no estimation.

`cache_eff_pct` is cost-weighted (per Q3-B). Token-ratio version was rejected because cache_read tokens are ~10× cheaper than input — token efficiency overstates dollar savings.

### Helper module

New `report/_cache.py`:

```python
def cache_metrics(
    rows: Iterable[CacheRow],  # model, input, cache_create, cache_read
    resolve_pricing: Callable[[str], Pricing | None],
) -> CacheMetrics:
    """Returns savings_usd, eff_pct, and rows_excluded_for_unknown_model."""
```

Aggregates per-model (so each model's rates are applied correctly), then sums. Models with no pricing data are excluded and counted; the count drives a footer warning.

### Surfaces

- `ccforensics session show <id>` — new totals line:
  ```
  Cache: 12.4M read · 1.2M created · 87.3% efficiency · saved $3.42
  ```
- `ccforensics aggregate` — same line in the totals footer.
- JSON: `cache_read_tokens`, `cache_creation_tokens`, `cache_eff_pct`, `cache_savings_usd` at top level.
- CSV: same keys as columns.

### Edge cases

- Zero `cache_read` → `eff_pct = 0.0`, `savings_usd = 0.0`. Render as `—` in text, `0.0` in JSON/CSV (preserves type stability).
- Pricing miss for a model → that model excluded, footer warning surfaced (no fabrication).

### Not surfaced

- Per-bucket cache eff (cache is request-level, not bucket-level).
- Per-tool cache eff (same reason).

## 5. Per-tool / per-MCP report

### CLI

```
ccforensics tools [OPTIONS]

  --session ID       scope to one session (uses report.resolver)
  --days N           scope to last N days (uses report._dates)
  --since DATE       absolute lower bound
  --until DATE       absolute upper bound
  --detail           expand mcp__<server>__* rows into per-tool rows
  --top N            keep top N rows by isolated_cost (default 50)
  --sort COL         isolated_cost (default) | invocations | shared_exposure
  --json             JSON output (mutually exclusive with --csv)
  --csv              CSV output
  --no-refresh       skip the index refresh that runs by default
```

Conventions match `session show`, `aggregate`, `plugins`.

### Default render — server-rolled

```
TOOL / MCP SERVER         INVOCATIONS  ISOLATED TURNS  ISOLATED $   SHARED TURNS  SHARED $≤
────────────────────────  ───────────  ──────────────  ──────────  ────────────  ─────────
Edit                            1,247           1,103       12.04            144      1.78
Bash                              892             801        8.91             91      1.12
Read                            2,103           2,041        4.22             62      0.81
mcp__stratplaybook (server)       142             118        3.20             24      0.42
mcp__strattrader-coll. (server)    87              79        2.11              8      0.19
TOTALS                          4,471           4,142       30.48            329      4.32
```

`--detail` expands `mcp__<server> (server)` rows to per-tool rows.

### Column semantics

| Column | Definition | Precision |
|---|---|---|
| `invocations` | `COUNT(*)` from `message_tool_uses` matching tool/server | exact |
| `isolated_turns` | distinct `message_dedup_key` where this tool/server is the only tool emitted in that turn | exact |
| `isolated_cost_usd` | `SUM(messages.cost_usd)` over those isolated turns | **exact** |
| `shared_turns` | distinct `message_dedup_key` where this tool/server appears alongside ≥1 sibling tool | exact |
| `shared_exposure_usd` | `SUM(messages.cost_usd)` over those shared turns | **upper bound** — same turn cost may appear under sibling tools; do not sum across rows |

### Footer note (always rendered in text + JSON `_meta`)

> Isolated $ is exact. Shared $ is an upper bound — when a turn emits multiple tools, the same turn cost appears under each sibling. Do not sum the Shared $ column across rows.

### SQL skeleton

```sql
WITH per_message_tool_count AS (
  SELECT message_dedup_key, COUNT(*) AS n_tools
  FROM message_tool_uses
  GROUP BY message_dedup_key
),
tool_keys AS (
  SELECT
    mtu.message_dedup_key,
    COALESCE(mtu.mcp_server, mtu.tool_name) AS group_key,  -- swap to mtu.tool_name when --detail
    CASE WHEN mtu.mcp_server IS NOT NULL THEN 'mcp_server' ELSE 'native' END AS group_kind
  FROM message_tool_uses mtu
)
SELECT
  tk.group_key,
  tk.group_kind,
  COUNT(*) AS invocations,
  COUNT(DISTINCT CASE WHEN pmtc.n_tools = 1 THEN tk.message_dedup_key END) AS isolated_turns,
  COALESCE(SUM(CASE WHEN pmtc.n_tools = 1 THEN m.cost_usd ELSE 0 END), 0) AS isolated_cost_usd,
  COUNT(DISTINCT CASE WHEN pmtc.n_tools > 1 THEN tk.message_dedup_key END) AS shared_turns,
  COALESCE(SUM(CASE WHEN pmtc.n_tools > 1 THEN m.cost_usd ELSE 0 END), 0) AS shared_exposure_usd
FROM tool_keys tk
JOIN per_message_tool_count pmtc USING (message_dedup_key)
JOIN messages m ON m.dedup_key = tk.message_dedup_key
WHERE m.session_id IN (:session_ids)
GROUP BY tk.group_key, tk.group_kind
ORDER BY isolated_cost_usd DESC
LIMIT :top;
```

`--detail` swaps the `group_key` expression to `mtu.tool_name`.

### Invariant

The per-tool report does NOT participate in the bucket-attribution invariant. It's a different slicing of the same cost data. Sum of `isolated_cost_usd` across all tools equals the sum of single-tool-turn costs in `messages` — exact equality, asserted in tests. `shared_exposure_usd` is intentionally not summable.

## 6. Service-tier capture

### What ships

- `messages.service_tier` populated from `usage.service_tier` on every reconciled message.
- Current corpus populates as `'standard'`. Future fast-mode sessions populate as whatever Anthropic emits (likely `'priority'`).
- No pricing branch. `pricing.resolve_pricing` returns standard-tier rates for everything.

### Tier visibility (read-only, no $ math)

- `session show` totals: if any non-standard tier present, append:
  ```
  Service tiers: standard 1,247 msgs · priority 38 msgs  (priority pricing not yet applied)
  ```
- `aggregate` totals: same line if any session contains non-standard tier.
- JSON: top-level `service_tier_breakdown: {standard: 1247, priority: 38}`.

### Explicitly NOT shipping

- Multiplier in `pricing.py`. Until a real fast-mode session lands and the signal is verified, no pricing branch.
- Per-tier $ totals. Showing dollars that are wrong-by-design violates charter.

### Follow-up checklist (for the future re-spec)

When the breakdown line surfaces a non-standard tier:

1. Inspect a fast-mode message — confirm `usage.service_tier == 'priority'` (or whatever Claude Code emits).
2. Check LiteLLM pricing JSON for priority-tier model entries (e.g., `claude-opus-4-6-priority`). If present, the existing fuzzy resolver may handle it for free.
3. If absent, add hardcoded fallback rates from Anthropic's published priority-tier pricing.
4. Add per-tier $ rollup to reports.

## 7. Testing, migration, rollout

### Schema migration (v2 → v3)

```python
# index.py MIGRATIONS list, appended:
[
    "ALTER TABLE messages ADD COLUMN service_tier TEXT",
    """CREATE TABLE message_tool_uses (
        message_dedup_key  TEXT NOT NULL REFERENCES messages(dedup_key) ON DELETE CASCADE,
        ordinal            INTEGER NOT NULL,
        tool_use_id        TEXT NOT NULL,
        tool_name          TEXT NOT NULL,
        mcp_server         TEXT,
        args_size_bytes    INTEGER NOT NULL,
        PRIMARY KEY (message_dedup_key, ordinal)
    )""",
    "CREATE INDEX idx_mtu_tool_name ON message_tool_uses(tool_name)",
    "CREATE INDEX idx_mtu_mcp_server ON message_tool_uses(mcp_server) WHERE mcp_server IS NOT NULL",
    "UPDATE files SET mtime_ns = 0",  # force cold backfill on next reconcile
]
```

`PRAGMA user_version = 3` after migration list applies. Existing migration runner unchanged.

### New test files

`tests/test_jsonl_multi_tool.py`:
- Synthetic assistant entry with 3 tool_use blocks (1 native, 2 `mcp__server__*`).
- Assert all 3 rows land in `message_tool_uses` with correct `ordinal`, `mcp_server`, `args_size_bytes`.
- Assert `messages.tool_name` still equals first tool's name (regression guard for `tree.discover_spawn`).

`tests/test_service_tier.py`:
- Synthetic usage block with `service_tier='priority'` round-trips into `messages.service_tier`.
- Synthetic message with `service_tier='priority'` → `pricing.resolve_pricing` returns standard rates (regression guard against silently adding a multiplier).
- `service_tier_breakdown` correctly counts a mixed-tier session.

`tests/test_cache_metrics.py`:
- Known cache_read/create/input + known prices → exact `cache_savings_usd` and `cache_eff_pct`.
- Zero cache_read → `eff_pct == 0.0`, `savings_usd == 0.0`.
- Unknown model in pricing → that row excluded from calc, footer warning surfaced.

`tests/test_tools_report.py`:
- Fixture: 3 sessions, mix of single-tool and multi-tool turns, mix of native + mcp tools.
- Assert `isolated_cost_usd` per tool sums to exactly the cost of single-tool turns containing that tool.
- Assert `shared_exposure_usd` equals the sum of multi-tool turn costs containing the tool (overlap with siblings is intentional and verified).
- Assert sum of `isolated_cost_usd` across all tools equals the sum of single-tool-turn costs in `messages` — exact equality.
- Assert `--detail` expands `mcp_server` rows to per-tool rows correctly.
- Assert `--top N` clamps row count.
- Assert `--sort` parameter respected.

### Existing tests touched

`tests/test_attribution.py`:
- Add assertion that the bucket invariant `Σ(session_rollups.cost_usd) == Σ(messages.cost_usd)` still holds after schema v3 migration runs (sanity guard against migration side effects).

### Real-data fixture

`tests/fixtures/real/redacted_session.jsonl`:
- Re-run `scripts/redact_jsonl.py` so the regenerated fixture includes `service_tier` from the source. Add `service_tier` to the redaction allow-list — it's metadata, not user content.

### CI gates

- `uv run pytest --cov=ccforensics --cov-report=term-missing --cov-fail-under=85` — new code targets ≥85%.
- `uv run mypy src/` — strict; new pydantic field typed; new SQL helpers typed.
- `uv run ruff check` and `uv run ruff format --check` clean.

### Dependencies

No new dependencies. SQLite stdlib + existing pydantic + click.

### Rollout

- README addition: brief `tools` command example, cache-eff line in sample `session show` output, note about `service_tier` capture being precursor to fast-mode pricing.
- CHANGELOG entry under `[Unreleased]`:
  - Added: `tools` command (per-tool / per-MCP spend with isolated/shared honesty).
  - Added: cache efficiency + cache savings columns in `session show` and `aggregate`.
  - Added: `service_tier` capture (pricing branch deferred until fast-mode signal verified).
  - Schema: v2 → v3 migration runs automatically; first command after upgrade triggers full re-reconcile (one-shot).

### No deprecations, no breaking CLI changes.
